"""Local bootstrap entry point. Snapshot preparation is explicitly unimplemented."""

import argparse
import ipaddress
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from 主程序.repositories.database import DatabaseError, database_health, initialize_database
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap only: business tables and snapshot preparation are not implemented.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-db", help="explicitly initialize a new bootstrap-only database")
    init.add_argument("--database", type=database_path, default=DEFAULT_DATABASE)
    serve = commands.add_parser("serve", help="serve read-only schema/FK health; requires init-db")
    serve.add_argument("--database", type=database_path, default=DEFAULT_DATABASE)
    serve.add_argument("--host", type=loopback_host, default="127.0.0.1")
    serve.add_argument("--port", type=valid_port, default=8000)
    commands.add_parser("prepare-snapshot", help="not implemented; exits nonzero and does not scan")
    args = parser.parse_args(argv)
    if args.command == "prepare-snapshot":
        print("NOT_IMPLEMENTED: snapshot preparation is not implemented; no scan was performed.", file=sys.stderr)
        return 2
    try:
        if args.command == "init-db":
            created = initialize_database(args.database)
            print("INITIALIZED bootstrap-only schema version 1" if created else "ALREADY_INITIALIZED bootstrap-only schema version 1")
        else:
            database_health(args.database)
            import uvicorn
            uvicorn.run(create_app(args.database), host=args.host, port=args.port, workers=1,
                        loop="asyncio", http="h11", ws="none")
    except DatabaseError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
