import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tomllib

from fastapi.testclient import TestClient
from packaging.requirements import Requirement
import pytest

from 主程序.cli import main
from 主程序.repositories.database import initialize_database
from 主程序.web.app import PROJECT_ROOT, create_app


def test_bootstrap_app_lifespan_health_and_request_id(tmp_path):
    path = tmp_path / "bootstrap.sqlite3"
    initialize_database(path)
    before = path.read_bytes()
    app = create_app(path)
    assert app.state.started is False
    with TestClient(app) as client:
        assert app.state.started is True
        first = client.get("/health", headers={"X-Request-ID": str(path).encode("utf-8")})
        second = client.get("/health")
        assert first.status_code == second.status_code == 200
        assert first.json()["schema_version"] == 1
        assert first.json()["foreign_keys"] is True
        assert first.json()["scope"] == "bootstrap-only"
        assert first.json()["request_id"] == first.headers["X-Request-ID"]
        assert len(first.json()["request_id"]) == 32
        assert first.json()["request_id"] != second.json()["request_id"]
        assert str(path) not in first.text
    assert app.state.started is False
    assert before == path.read_bytes()


@pytest.mark.parametrize("kind", ["missing", "unknown", "future"])
def test_bootstrap_health_rejects_missing_unknown_future_without_creating(kind, tmp_path):
    path = tmp_path / "database.sqlite3"
    if kind != "missing":
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version = 2" if kind == "future" else "CREATE TABLE unrelated (id INTEGER)")
        connection.close()
    before = path.read_bytes() if path.exists() else None
    with TestClient(create_app(path)) as client:
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["error"]["details"] == {}
        assert str(path) not in response.text
    assert (path.read_bytes() if path.exists() else None) == before


def test_bootstrap_cli_direct_file_from_unrelated_cwd(tmp_path):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    path = tmp_path / "db.sqlite3"
    command = [sys.executable, str(PROJECT_ROOT / "主程序/cli.py")]
    # B02 now exists; missing required --output remains argparse exit 2, not NOT_IMPLEMENTED.
    for args, expected in [(["--help"], 0), (["init-db", "--database", str(path)], 0), (["prepare-snapshot"], 2), (["not-a-command"], 2), (["serve", "--unknown"], 2)]:
        result = subprocess.run(command + args, cwd=tmp_path, env=env, capture_output=True, timeout=15)
        assert result.returncode == expected
    assert path.exists()


def test_bootstrap_cli_module_help_and_unknown_args(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT), "PYTHONIOENCODING": "utf-8"}
    for args, expected in [(["--help"], 0), (["init-db", "--unknown"], 2)]:
        result = subprocess.run([sys.executable, "-m", "主程序.cli", *args], cwd=tmp_path, env=env, capture_output=True, timeout=15)
        assert result.returncode == expected


@pytest.mark.parametrize("args", [["serve", "--host", "0.0.0.0"], ["serve", "--host", "192.168.1.1"], ["serve", "--host", "localhost"], ["serve", "--port", "0"], ["serve", "--port", "65536"], ["serve", "--port", "abc"], ["serve", "--workers", "2"]])
def test_bootstrap_cli_rejects_nonloopback_invalid_ports_unknown_options(args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code != 0


def test_bootstrap_cli_serve_is_single_worker_and_requires_explicit_init(tmp_path, monkeypatch):
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    path = tmp_path / "db.sqlite3"
    assert main(["serve", "--database", str(path)]) == 1
    assert not path.exists()
    assert calls == []
    assert main(["init-db", "--database", str(path)]) == 0
    assert main(["serve", "--database", str(path)]) == 0
    assert calls == [{"host": "127.0.0.1", "port": 8000, "workers": 1, "loop": "asyncio", "http": "h11", "ws": "none"}]


def test_bootstrap_cli_relative_path_is_project_relative(tmp_path, monkeypatch):
    import 主程序.cli as cli
    monkeypatch.chdir(tmp_path)
    assert cli.database_path("数据/runtime/test.sqlite3") == PROJECT_ROOT / "数据/runtime/test.sqlite3"


def test_bootstrap_lock_exact_versions_and_complete_active_dependency_closure():
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == ">=3.12,<3.14"
    locked = {}
    for line in (PROJECT_ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        assert len(list(requirement.specifier)) == 1
        spec = next(iter(requirement.specifier))
        assert spec.operator == "==" and "*" not in spec.version
        locked[requirement.name.lower().replace("_", "-")] = spec.version
        assert importlib.metadata.version(requirement.name) == spec.version
    for name in locked:
        for raw in importlib.metadata.requires(name) or []:
            dependency = Requirement(raw)
            if dependency.marker and not dependency.marker.evaluate({"extra": ""}):
                continue
            normalized = dependency.name.lower().replace("_", "-")
            assert normalized in locked, f"missing transitive dependency: {name} -> {normalized}"
            assert locked[normalized] in dependency.specifier
    for requirement in project["project"]["dependencies"] + project["project"]["optional-dependencies"]["test"]:
        dependency = Requirement(requirement)
        assert locked[dependency.name.lower().replace("_", "-")] in dependency.specifier
