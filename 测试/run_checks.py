"""Run real checks and preserve uniquely named evidence, including failures.

Schema v2 corrects the historical label-derived normalization version and the
Python-only source inventory. Historical evidence is deliberately not rewritten.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from uuid import uuid4

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def bind_project():
    """Prefer this checkout even when the supplied venv has another editable install."""
    sys.path.insert(0, str(ROOT))
    origins = {}
    for name in ("主程序", "测试"):
        module = importlib.import_module(name)
        locations = ([Path(module.__file__).resolve()] if module.__file__ else
                     [Path(p).resolve() for p in module.__path__])
        if not locations or any(not p.is_relative_to(ROOT / name) for p in locations):
            raise RuntimeError(f"FOREIGN_PROJECT_IMPORT: {name}")
        # 测试 is a namespace package in the existing project, so __file__ is None.
        origins[name] = {"file": module.__file__, "locations": [str(p) for p in locations]}
    return origins


def source_digest(root=None):
    root = ROOT if root is None else Path(root)
    extensions = {".py", ".html", ".js", ".css", ".json", ".toml", ".ini", ".cfg", ".yaml", ".yml"}
    files = set()
    for directory in (root / "主程序", root / "测试"):
        for p in directory.rglob("*"):
            if p.is_file() and p.suffix.lower() in extensions and not {"__pycache__", "evidence"}.intersection(p.relative_to(directory).parts):
                files.add(p)
    for name in ("pyproject.toml", "requirements.lock", "pytest.ini", "setup.cfg", "tox.ini", ".coveragerc"):
        if (root / name).is_file():
            files.add(root / name)
    for p in root.glob("*.json"):
        if p.is_file():
            files.add(p)
    records = [{"path": p.relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
               for p in sorted(files)]
    digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    return digest, records


def input_records(paths):
    return [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None} for p in paths]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--input-kind", default="pure-synthetic")
    parser.add_argument("--input", type=Path, action="append", default=[])
    parser.add_argument("--scope", default="Explicit command only; not complete project acceptance")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a real command is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        parser.error("label must be a simple file-name component")
    if not args.evidence_root.is_dir() or not args.evidence_root.resolve().is_relative_to(ROOT):
        parser.error("evidence root must already exist inside this checkout")
    origins = bind_project()
    from 主程序.domain.non_product import NORMALIZATION_VERSION
    output = args.evidence_root / (args.label + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir()
    digest, sources = source_digest()
    base = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8")
    status = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True, encoding="utf-8")
    input_before = input_records(args.input)
    start = datetime.now(timezone.utc).isoformat()
    local_start = datetime.now().astimezone().isoformat()
    clock = time.perf_counter()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(ROOT)}
    timed_out = False
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, timeout=args.timeout)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        result = subprocess.CompletedProcess(command, 124, exc.stdout or b"", exc.stderr or b"")
    except OSError as exc:
        result = subprocess.CompletedProcess(command, 127, b"", str(exc).encode("utf-8"))
    end = datetime.now(timezone.utc).isoformat()
    elapsed = time.perf_counter() - clock
    after_digest, after_sources = source_digest()
    input_after = input_records(args.input)
    unchanged = digest == after_digest
    runner_exit = result.returncode or (3 if not unchanged else 4 if input_before != input_after else 0)
    (output / "stdout.txt").write_bytes(result.stdout)
    (output / "stderr.txt").write_bytes(result.stderr)
    (output / "command.txt").write_text(subprocess.list2cmdline(command), encoding="utf-8")
    fixture = ROOT / "数据/fixtures/controlled_cases.json"
    metadata = {
        "evidence_schema_version": 2,
        "historical_correction": "v1 label-derived normalization and Python-only source digests are not authoritative; old records unchanged",
        "label": args.label, "command": command, "cwd": str(ROOT), "started_at": start,
        "started_at_local": local_start, "finished_at": end, "finished_at_local": datetime.now().astimezone().isoformat(),
        "duration_seconds": elapsed, "exit_code": result.returncode, "runner_exit_code": runner_exit, "timed_out": timed_out,
        "input_kind": args.input_kind, "input_files": input_after, "input_files_before": input_before,
        "input_unchanged": input_before == input_after,
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest() if fixture.is_file() else None,
        "normalization_version": NORMALIZATION_VERSION, "normalization_constant": "主程序.domain.non_product.NORMALIZATION_VERSION",
        "import_origins": origins,
        "base_commit": base.stdout.strip(), "git_status": status.stdout,
        "source_tree_digest": digest, "source_tree_digest_before": digest, "source_tree_digest_after": after_digest,
        "source_unchanged": unchanged, "source_files": sources, "source_files_after": after_sources,
        "python": sys.version, "executable": sys.executable, "platform": platform.platform(), "machine": platform.machine(),
        "processor": platform.processor(), "cpu_count": os.cpu_count(),
        "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "scope": args.scope,
    }
    (output / "result.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result.stdout.decode("utf-8", errors="replace"), end="")
    print(result.stderr.decode("utf-8", errors="replace"), end="", file=sys.stderr)
    print(f"Evidence: {output}; command_exit_code={result.returncode}; runner_exit_code={runner_exit}")
    return runner_exit


if __name__ == "__main__":
    raise SystemExit(main())
