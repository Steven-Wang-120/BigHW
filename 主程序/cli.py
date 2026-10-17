"""Local B02-B04 scan/import/reconcile and B05-B06 authenticated workflow commands."""

import argparse
import getpass
import ipaddress
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uuid import uuid4

from 主程序.repositories.database import DatabaseError, database_health, initialize_database, upgrade_database
from 主程序.repositories.xlsx_reader import InputError
from 主程序.services.recovery_service import recover_jobs
from 主程序.services.snapshot_service import prepare_snapshot
from 主程序.services.import_service import import_snapshot, counts
from 主程序.services.reconcile_service import reconcile, run_summary
from 主程序.web.app import DEFAULT_DATABASE, PROJECT_ROOT, create_app


def loopback_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("host must be a literal loopback IP address") from exc
    if not address.is_loopback:
        raise argparse.ArgumentTypeError("only loopback hosts are allowed")
    return str(address)


def valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def database_path(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def windows_console_available(streams) -> bool:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_console_mode = kernel32.GetConsoleMode
    get_console_mode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_console_mode.restype = wintypes.BOOL
    try:
        for stream in streams:
            handle = msvcrt.get_osfhandle(stream.fileno())
            mode = wintypes.DWORD()
            if not get_console_mode(wintypes.HANDLE(handle), ctypes.byref(mode)):
                return False
    except (AttributeError, OSError, ValueError):
        return False
    return True


def interactive_password_available() -> bool:
    streams = (sys.stdin, sys.stderr)
    if not all(stream.isatty() for stream in streams):
        return False
    # NUL is a Windows character device (isatty=True), but not a console.
    return sys.platform != "win32" or windows_console_available(streams)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local B02-B08 scan/import/reconcile and authenticated workflow commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-db", help="explicitly initialize a new bootstrap-only database")
    init.add_argument("--database", type=database_path, default=DEFAULT_DATABASE)
    serve = commands.add_parser("serve", help="serve local health, evidence, and authenticated workflow API; requires init-db")
    serve.add_argument("--database", type=database_path, default=DEFAULT_DATABASE)
    serve.add_argument("--host", type=loopback_host, default="127.0.0.1")
    serve.add_argument("--port", type=valid_port, default=8000)
    serve.add_argument("--app-origin", help="explicit trusted browser origin; default is the actual HTTP host/port")
    admin = commands.add_parser("create-admin", help="initialize first admin in an empty v4 user store; password via getpass only")
    admin.add_argument("--database", type=database_path, required=True)
    admin.add_argument("--username", required=True)
    scan = commands.add_parser("prepare-snapshot", help="complete read-only ZIP/XML scan and deterministic 10k raw snapshot")
    scan.add_argument("--source", type=database_path, default=PROJECT_ROOT / "数据/raw/uci_online_retail_ii/online_retail_II.xlsx")
    scan.add_argument("--archive", type=database_path, default=PROJECT_ROOT / "数据/raw/uci_online_retail_ii/online+retail+ii.zip")
    scan.add_argument("--output", type=database_path, required=True)
    scan.add_argument("--quality-version", choices=("quality-v2-row-invoice-range",), default=None)
    upgrade = commands.add_parser("upgrade-db", help="explicitly upgrade a verified database to v2, v3, or v4 (default target: v2)")
    upgrade.add_argument("--database", type=database_path, required=True)
    upgrade.add_argument("--target-version", type=int, choices=(2, 3, 4), default=2, help="explicit target schema version (default: 2)")
    imp = commands.add_parser("import-snapshot", help="validate trusted snapshot and import transactionally")
    imp.add_argument("--database", type=database_path, required=True)
    imp.add_argument("--snapshot", type=database_path, required=True)
    imp.add_argument("--source", type=database_path, default=PROJECT_ROOT / "数据/raw/uci_online_retail_ii/online_retail_II.xlsx")
    imp.add_argument("--manifest-digest", required=True)
    count = commands.add_parser("counts", help="recompute and verify read-only aggregate counts")
    count.add_argument("--database", type=database_path, required=True)
    count.add_argument("--batch-id", required=True)
    rec = commands.add_parser("reconcile", help="reconcile a successful batch using fixed D-03 rule; schema3 or schema4 required")
    rec.add_argument("--database", type=database_path, required=True)
    rec.add_argument("--batch-id", required=True)
    summary = commands.add_parser("run-summary", help="read only a completed reconciliation summary")
    summary.add_argument("--database", type=database_path, required=True)
    summary.add_argument("--run-id", required=True)
    recovery = commands.add_parser("recover-jobs", help="recover cleanly interrupted schema-4 jobs")
    recovery.add_argument("--database", type=database_path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init-db":
            created = initialize_database(args.database)
            print("INITIALIZED bootstrap-only schema version 1" if created else "ALREADY_INITIALIZED bootstrap-only schema version 1")
        elif args.command == "create-admin":
            from uuid import uuid4
            from 主程序.services.auth_service import create_user
            if not interactive_password_available():
                raise InputError("INTERACTIVE_PASSWORD_REQUIRED", 422)
            password = getpass.getpass("New admin password (8–72 UTF-8 bytes): ")
            confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                raise InputError("PASSWORD_CONFIRMATION_FAILED", 422)
            user = create_user(args.database, None, args.username, password, "ADMIN", uuid4().hex, bootstrap=True)
            print(json.dumps({"user": user}, ensure_ascii=False))
        elif args.command == "upgrade-db":
            changed = upgrade_database(args.database, target_version=args.target_version)
            version = database_health(args.database)["schema_version"]
            print(("UPGRADED" if changed else "ALREADY_UPGRADED") + f" business schema version {version}")
        elif args.command == "prepare-snapshot":
            print(json.dumps(prepare_snapshot(args.source, args.output, archive=args.archive, quality_version=args.quality_version), ensure_ascii=False, indent=2))
        elif args.command == "import-snapshot":
            print(json.dumps(import_snapshot(args.database, args.snapshot, args.source, args.manifest_digest), ensure_ascii=False, indent=2))
        elif args.command == "counts":
            print(json.dumps(counts(args.database, args.batch_id), ensure_ascii=False, indent=2))
        elif args.command == "reconcile":
            print(json.dumps(reconcile(args.database, args.batch_id), ensure_ascii=False, indent=2))
        elif args.command == "run-summary":
            print(json.dumps(run_summary(args.database, args.run_id), ensure_ascii=False, indent=2))
        elif args.command == "recover-jobs":
            print(json.dumps(recover_jobs(args.database, request_id=uuid4().hex), ensure_ascii=False, indent=2))
        else:
            database_health(args.database)
            import uvicorn
            host = f"[{args.host}]" if ":" in args.host else args.host
            origin = args.app_origin or f"http://{host}:{args.port}"
            from 主程序.web.api import canonical_origin
            try:
                canonical_origin(origin)
            except ValueError:
                raise InputError("APP_ORIGIN_INVALID", 422)
            uvicorn.run(create_app(args.database, app_origin=origin), host=args.host, port=args.port, workers=1,
                        loop="asyncio", http="h11", ws="none", access_log=False)
    except InputError as exc:
        print(json.dumps({"error": {"code": exc.code, "details": exc.details}, "status": exc.status}), file=sys.stderr)
        return 1
    except DatabaseError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
