"""Pure F/script tests only; none prove a new Claude CLI Hook/Skill invocation."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / ".claude/hooks/danger_guard.py"
HELPER = ROOT / ".claude/skills/run-app/scripts/run_app.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("b10_run_app", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sandbox(tmp_path):
    # Guard logs anchored to its own file, never an environment/stdin chosen path.
    target = tmp_path / ".claude/hooks/danger_guard.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(GUARD, target)
    return target


def feed(sandbox, command=None, *, tool="PowerShell", raw=None):
    if raw is None:
        raw = json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                          "tool_input": {"command": command}}, ensure_ascii=False).encode("utf-8")
    return subprocess.run([sys.executable, "-B", str(sandbox)], input=raw, capture_output=True, timeout=10)


DANGEROUS = [
    ("ps_recurse", "Remove-Item -LiteralPath 'F-only-empty' -Recurse"),
    ("ps_r", "remove-item F-only-empty -r"),
    ("ps_case_space", "  REMOVE-ITEM   -Recurse -Force 'F-only-empty'"),
    ("ps_switch_value", "Remove-Item F-only-empty -Recurse:$true"),
    ("posix_rf", "rm -rf F-only-empty"),
    ("posix_fr", "rm -fr F-only-empty"),
    ("posix_separate", "rm -f -r F-only-empty"),
    ("posix_long", "rm --recursive --force F-only-empty"),
    ("cmd_rmdir", "cmd /c rmdir /s /q F-only-empty"),
    ("cmd_rd", "RD /S F-only-empty"),
    ("reset", "git reset --hard HEAD"),
    ("reset_global", "git -C . reset --hard"),
    ("reset_spaces", "GIT\treset\t--hard HEAD"),
    ("clean", "git clean -f"),
    ("clean_combined", "git clean -xdf"),
    ("clean_long", "git clean --force"),
    ("push", "git push --force origin main"),
    ("push_short", "git push origin main -f"),
    ("push_lease", "git push --force-with-lease=refs/heads/main origin main"),
    ("policy", "powershell -ExecutionPolicy Bypass -File F.ps1"),
    ("policy_short", "pwsh -ep bypass -File F.ps1"),
    ("policy_colon", "pwsh -ExecutionPolicy:Bypass -File F.ps1"),
    ("set_policy", "Set-ExecutionPolicy -Scope Process Bypass"),
    ("compound", "git status; git reset --hard HEAD"),
]


@pytest.mark.parametrize("command", [value for _, value in DANGEROUS], ids=[name for name, _ in DANGEROUS])
@pytest.mark.parametrize("tool", ["PowerShell", "Bash"])
def test_denies_json_only_never_executes(sandbox, command, tool):
    result = feed(sandbox, command, tool=tool)
    assert result.returncode == 0 and result.stderr == b""
    output = json.loads(result.stdout)
    assert set(output) == {"hookSpecificOutput"}
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert set(output["hookSpecificOutput"]) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}


@pytest.mark.parametrize("command", [
    "git status --short", "git reset --soft HEAD~1", "git clean -n", "git push --dry-run origin main",
    "Remove-Item -LiteralPath F-single-file.txt", "Get-ChildItem -Recurse .", "rm F-single-file.txt",
    "rmdir F-only-empty", "git push --force-if-includes", "pwsh -ExecutionPolicy RemoteSigned -File F.ps1",
    "python -B .claude/skills/run-app/scripts/run_app.py status --state F.json",
    "Get-Date; Get-Command python", "git reset --harder", "myrm -rf F-not-command",
])
def test_normal_is_silent_no_decision(sandbox, command):
    result = feed(sandbox, command)
    assert result.returncode == 0 and result.stdout == result.stderr == b""
    assert not list(sandbox.parents[2].rglob("denial.json"))


def test_wrong_tool_is_no_decision(sandbox):
    result = feed(sandbox, "Remove-Item -Recurse F", tool="Read")
    assert result.returncode == 0 and result.stdout == b""


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b"{}", b"\xff", b"{" * 10000,
    b'{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":4}}',
    b'{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_name":"Bash"}',
    b"x" * 65537], ids=["invalid-json", "array", "missing-event", "bad-utf8", "deep-json", "nonstring-command", "duplicate-key", "oversize"])
def test_bad_shape_size_denied_without_input_echo(sandbox, raw):
    result = feed(sandbox, raw=raw)
    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert result.stderr == b""


def test_hash_only_unique_log_no_secrets(sandbox):
    secret = "F-SECRET-DO-NOT-LOG-731"
    command = "Remove-Item -Recurse 'F-only-empty-" + secret + "'"
    for _ in range(2):
        result = feed(sandbox, command)
        assert result.returncode == 0 and secret.encode() not in result.stdout + result.stderr
    logs = list(sandbox.parents[2].rglob("denial.json"))
    assert len(logs) == 2 and logs[0].parent != logs[1].parent
    for path in logs:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
        assert secret not in raw and command not in raw
        assert set(value) == {"utc", "tool", "pattern", "command_sha256", "reason"}
        assert value["command_sha256"] == hashlib.sha256(command.encode()).hexdigest()
        assert value["utc"].endswith("+00:00")


def test_project_settings_exec_matcher_and_no_permissions():
    settings = json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    assert set(settings) == {"hooks"}
    assert set(settings["hooks"]) == {"PreToolUse"}
    entry = settings["hooks"]["PreToolUse"][0]
    assert all(re.fullmatch(entry["matcher"], name) for name in ("PowerShell", "Bash"))
    assert not re.fullmatch(entry["matcher"], "Read")
    hook = entry["hooks"][0]
    assert hook["type"] == "command" and hook["command"] == "python"
    assert hook["args"] == ["-B", "${CLAUDE_PROJECT_DIR}/.claude/hooks/danger_guard.py"]
    # Exec-vector pipe-test of the actual settings hook, not a dangerous command.
    args = [arg.replace("${CLAUDE_PROJECT_DIR}", str(ROOT)) for arg in hook["args"]]
    p = subprocess.run([hook["command"], *args], input=json.dumps({"hook_event_name": "PreToolUse",
                       "tool_name": "Bash", "tool_input": {"command": "git status"}}).encode(), capture_output=True, timeout=10)
    assert p.returncode == 0 and p.stdout == p.stderr == b""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process ownership")
def test_helper_windows_argument_and_handle_identity():
    helper = load_helper()
    args = ["-B", str(ROOT / "主程序/cli.py"), "serve", "--database", "C:\\F path\\db.sqlite3"]
    assert helper.command_arguments(subprocess.list2cmdline([sys.executable, *args]))[1:] == args
    with helper.process_handle(os.getpid()) as (kernel, handle):
        identity = helper.handle_identity(kernel, handle)
    assert identity["creation_ticks"] > 0 and Path(identity["executable"]).is_file()
    own = {"pid": 12, "parent_pid": 11, "arguments": args}
    helper.confirm(own, {"ProcessId": 12, "ParentProcessId": 11, "CommandLine": subprocess.list2cmdline([sys.executable, *args])})
    with pytest.raises(RuntimeError, match="OWNERSHIP_MISMATCH"):
        helper.confirm(own, {"ProcessId": 12, "ParentProcessId": 10, "CommandLine": subprocess.list2cmdline([sys.executable, *args])})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows lifecycle")
def test_helper_f_v4_start_status_owned_stop_and_port_conflict():
    helper = load_helper()
    from 主程序.repositories.database import initialize_database, upgrade_database
    # Always test under this checkout's runtime, including default full pytest;
    # never require pytest's shared/default tmp_path to be a production DB root.
    directory = helper.new_directory()
    db = directory / "F-fresh.sqlite3"
    initialize_database(db)
    upgrade_database(db, target_version=4)
    before = helper.sha(db)
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
        p = subprocess.run([sys.executable, "-B", str(HELPER), "start", "--database", str(db), "--port", str(port),
                            "--python", sys.executable], capture_output=True, timeout=60)
        assert p.returncode != 0
        assert helper.sha(db) == before
    p = subprocess.run([sys.executable, "-B", str(HELPER), "start", "--database", str(db), "--port", str(port),
                        "--python", sys.executable], capture_output=True, timeout=80)
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    result = json.loads(p.stdout)
    state_path = Path(result["state"])
    try:
        assert result["status"] == "READY" and result["listener_pids"]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["health"]["foreign_keys"] is True and state["health"]["schema_version"] == 4
        assert all(p["parent_pid"] == state["launcher_pid"] for p in state["owned_processes"][1:])
        status = subprocess.run([sys.executable, "-B", str(HELPER), "status", "--state", str(state_path)], capture_output=True, timeout=45)
        assert status.returncode == 0
    finally:
        stopped = subprocess.run([sys.executable, "-B", str(HELPER), "stop", "--state", str(state_path)], capture_output=True, timeout=90)
        assert stopped.returncode == 0, stopped.stderr.decode(errors="replace")
        record = json.loads(stopped.stdout)
        assert record["owned_pids_absent"] and record["port_has_no_listener"]
    assert helper.sha(db) == before
    print("F helper start/status/stop evidence:", state_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ownership guard")
def test_stop_refuses_foreign_command_before_any_termination(monkeypatch):
    helper = load_helper()
    own = {"pid": 42, "parent_pid": 41, "arguments": ["-B", "F-own-cli.py"],
           "creation_ticks": 100, "executable": "F-python.exe"}
    monkeypatch.setattr(helper, "inventory", lambda _: {"processes": [{"ProcessId": 42, "ParentProcessId": 41,
                        "CommandLine": subprocess.list2cmdline([sys.executable, "-B", "F-foreign-cli.py"])}], "listeners": []})
    def must_not_open(*args, **kwargs):
        pytest.fail("must refuse foreign command before opening any termination handle")
    monkeypatch.setattr(helper, "process_handle", must_not_open)
    with pytest.raises(RuntimeError, match="OWNERSHIP_MISMATCH_NO_STOP"):
        helper.stop_owned({"port": 8787, "owned_processes": [own]})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows helper preflight")
def test_helper_missing_and_legacy_db_not_modified(tmp_path):
    from 主程序.repositories.database import initialize_database
    helper = load_helper()
    legacy = tmp_path / "F-legacy.sqlite3"
    initialize_database(legacy)
    before = helper.sha(legacy)
    with pytest.raises(RuntimeError, match="V4_READONLY_PREFLIGHT"):
        helper.readonly_preflight(sys.executable, legacy)
    assert before == helper.sha(legacy)
    missing = tmp_path / "F-missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        helper.readonly_preflight(sys.executable, missing)
    assert not missing.exists()
