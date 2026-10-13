"""Run real checks and preserve uniquely named evidence, including failures."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def source_digest():
    files = sorted([* (ROOT / "主程序").rglob("*.py"), * (ROOT / "测试").rglob("*.py"), ROOT / "pyproject.toml", ROOT / "requirements.lock"])
    records = [{"path": p.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
    digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    return digest, records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a real command is required")
    if not args.evidence_root.is_dir():
        parser.error("evidence root must already exist")
    output = args.evidence_root / (args.label + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir()
    digest, sources = source_digest()
    base = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8")
    status = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True, encoding="utf-8")
    start = datetime.now(timezone.utc).isoformat()
    clock = time.perf_counter()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    timed_out = False
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, timeout=90)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        result = subprocess.CompletedProcess(command, 124, exc.stdout or b"", exc.stderr or b"")
    end = datetime.now(timezone.utc).isoformat()
    elapsed = time.perf_counter() - clock
    (output / "stdout.txt").write_bytes(result.stdout)
    (output / "stderr.txt").write_bytes(result.stderr)
    (output / "command.txt").write_text(subprocess.list2cmdline(command), encoding="utf-8")
    metadata = {
        "label": args.label, "command": command, "cwd": str(ROOT), "started_at": start,
        "finished_at": end, "duration_seconds": elapsed, "exit_code": result.returncode, "timed_out": timed_out,
        "fixture_kind": "pure-synthetic", "fixture_sha256": hashlib.sha256((ROOT / "数据/fixtures/controlled_cases.json").read_bytes()).hexdigest(),
        "normalization_version": "original-uncommitted" if "baseline" in args.label else "normalization-v0.1-hardened",
        "base_commit": base.stdout.strip(), "git_status": status.stdout,
        "source_tree_digest": digest, "source_files": sources, "python": sys.version,
        "executable": sys.executable, "platform": platform.platform(), "machine": platform.machine(),
        "processor": platform.processor(), "cpu_count": os.cpu_count(),
        "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "scope": "B01 bootstrap and scoped synthetic domain tests only; no complete TC-15/16/18/21/22/41/48, matching or real-data validation",
    }
    (output / "result.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result.stdout.decode("utf-8", errors="replace"), end="")
    print(result.stderr.decode("utf-8", errors="replace"), end="", file=sys.stderr)
    print(f"Evidence: {output}; exit_code={result.returncode}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
