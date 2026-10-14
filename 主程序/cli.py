"""Explicit local scan/import/reconcile commands; authenticated writes are not implemented."""

import argparse
import ipaddress
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from 主程序.repositories.database import DatabaseError, database_health, initialize_database, upgrade_database
from 主程序.repositories.xlsx_reader import InputError
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local B02/B03/B04; no authenticated write HTTP or workflow API.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-db", help="explicitly initialize a new bootstrap-only database")
    init.add_argument("--database", type=database_path, default=DEFAULT_DATABASE)
    serve = commands.add_parser("serve", help="serve read-only schema/FK health; requires init-db")
    serve.add_argument("--database", type=database_path, default=DEFAULT_DATABASE)
    serve.add_argument("--host", type=loopback_host, default="127.0.0.1")
    serve.add_argument("--port", type=valid_port, default=8000)
    scan = commands.add_parser("prepare-snapshot", help="complete read-only ZIP/XML scan and deterministic 10k raw snapshot")
    scan.add_argument("--source", type=database_path, default=PROJECT_ROOT / "数据/raw/uci_online_retail_ii/online_retail_II.xlsx")
    scan.add_argument("--archive", type=database_path, default=PROJECT_ROOT / "数据/raw/uci_online_retail_ii/online+retail+ii.zip")
    scan.add_argument("--output", type=database_path, required=True)
    upgrade = commands.add_parser("upgrade-db", help="explicitly upgrade verified bootstrap v1 to business import v2")
    upgrade.add_argument("--database", type=database_path, required=True)
    upgrade.add_argument("--target-version", type=int, choices=(2, 3), default=2)
    imp = commands.add_parser("import-snapshot", help="validate trusted snapshot and import transactionally")
    imp.add_argument("--database", type=database_path, required=True)
    imp.add_argument("--snapshot", type=database_path, required=True)
    imp.add_argument("--source", type=database_path, default=PROJECT_ROOT / "数据/raw/uci_online_retail_ii/online_retail_II.xlsx")
    imp.add_argument("--manifest-digest", required=True)
    count = commands.add_parser("counts", help="recompute and verify read-only aggregate counts")
    count.add_argument("--database", type=database_path, required=True)
    count.add_argument("--batch-id", required=True)
    rec = commands.add_parser("reconcile", help="reconcile successful batch using fixed D-03 rule; schema3 required")
    rec.add_argument("--database", type=database_path, required=True)
    rec.add_argument("--batch-id", required=True)
    summary = commands.add_parser("run-summary", help="read only a completed reconciliation summary")
    summary.add_argument("--database", type=database_path, required=True)
    summary.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init-db":
            created = initialize_database(args.database)
            print("INITIALIZED bootstrap-only schema version 1" if created else "ALREADY_INITIALIZED bootstrap-only schema version 1")
        elif args.command == "upgrade-db":
            changed = upgrade_database(args.database, target_version=args.target_version)
            version = database_health(args.database)["schema_version"]
            print(("UPGRADED" if changed else "ALREADY_UPGRADED") + f" business schema version {version}")
        elif args.command == "prepare-snapshot":
            print(json.dumps(prepare_snapshot(args.source, args.output, archive=args.archive), ensure_ascii=False, indent=2))
        elif args.command == "import-snapshot":
            print(json.dumps(import_snapshot(args.database, args.snapshot, args.source, args.manifest_digest), ensure_ascii=False, indent=2))
        elif args.command == "counts":
            print(json.dumps(counts(args.database, args.batch_id), ensure_ascii=False, indent=2))
        elif args.command == "reconcile":
            print(json.dumps(reconcile(args.database, args.batch_id), ensure_ascii=False, indent=2))
        elif args.command == "run-summary":
            print(json.dumps(run_summary(args.database, args.run_id), ensure_ascii=False, indent=2))
        else:
            database_health(args.database)
            import uvicorn
            uvicorn.run(create_app(args.database), host=args.host, port=args.port, workers=1,
                        loop="asyncio", http="h11", ws="none")
    except InputError as exc:
        print(json.dumps({"error": {"code": exc.code, "details": exc.details}, "status": exc.status}), file=sys.stderr)
        return 1
    except DatabaseError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
