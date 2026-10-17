---
name: run-app
description: Start, health-check, inspect and stop this project's local FastAPI v4 app with an explicit database and owned-process evidence. Use for local launch requests, not performance or full UI acceptance.
---

# Run the local app

Run commands from this checkout's root. Read root `README.md` v4 startup section and `主程序/cli.py --help` if flags changed. Use existing `.venv/Scripts/python.exe`; do not install an editable copy or substitute another checkout's source. There is no Node build.

## Database first

Require an explicit existing database under this checkout's `数据/runtime/`. Startup validates the strict v4 schema read-only; missing, legacy or unknown schema is a stop condition, not permission to migrate. Never start against protected historical original databases.

For an explicitly requested **new empty** environment only, create a unique directory and check the DB does not exist, then run:

```powershell
$py = (Resolve-Path '.venv/Scripts/python.exe').Path
$dir = Join-Path '数据/runtime' ('run-app-' + (Get-Date -Format yyyyMMddTHHmmss) + '-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $dir -ErrorAction Stop | Out-Null
$db = Join-Path $dir 'fresh.sqlite3'
if (Test-Path -LiteralPath $db) { throw 'New DB target already exists' }
& $py -B -m 主程序.cli init-db --database $db
if ($LASTEXITCODE) { throw 'init-db failed' }
& $py -B -m 主程序.cli upgrade-db --database $db --target-version 4
if ($LASTEXITCODE) { throw 'Explicit v4 upgrade failed' }
```

`upgrade-db` defaults to v2, so v4 must be explicit. Do not copy/upgrade an old original to make startup work. A new DB has no default accounts/passwords. First admin creation is **only in a real interactive terminal**:

```powershell
& $py -B -m 主程序.cli create-admin --database $db --username local-admin
```

The user enters/confirm their own password through getpass. No password flag, shell/environment/stdin secret, NUL or captured noninteractive fallback. If no terminal is available, report `INTERACTIVE_PASSWORD_REQUIRED` and hand that step to the user's terminal; health-only startup does not need an account.

## Start / status / stop (Windows)

Use the helper relative to project root. It runs the real CLI `serve --database ABSOLUTE_DB --host 127.0.0.1 --port PORT` with one worker. It performs a read-only preflight before starting, rejects occupied ports without stopping their owners, and records launcher plus actual listener child PID for Windows venv redirection.

```powershell
python -B .claude/skills/run-app/scripts/run_app.py start --database $db --port 8787
# Use the exact state path returned above, not a guessed PID:
python -B .claude/skills/run-app/scripts/run_app.py status --state $state
python -B .claude/skills/run-app/scripts/run_app.py stop --state $state
```

`$db` must be the explicit target chosen above; `$state` is the actual `state` field returned by start. For an isolated checkout without its own venv, start accepts `--python` pointing to an explicitly supplied existing project interpreter; source/imports still belong to this checkout. Never hard-code a developer's machine path in the skill.

The helper uses hidden Windows subprocesses (`CREATE_NO_WINDOW`, not `Start-Process`). If manually using `Start-Process`, include `-WindowStyle Hidden`; record the launcher and listener identity/parentage first. Never stop a PID discovered solely by port, image name or an old log. Stop rechecks command-line arguments, creation time, executable and recorded parent/child relationships; mismatch means refuse, not force. A failed health check only permits cleanup of this invocation's verified processes.

## Observe and report

A successful launch requires `/health` HTTP 200 JSON with `status=ok`, `schema_version=4` and `foreign_keys=true`, plus listener ownership. Open the returned origin `/login` if requested; use only actual user-supplied credentials, don't log passwords or transaction detail. Helper evidence lives in `数据/runtime/B10-run-app/<actual-local-date>/<unique-run>/` (state/stdout/stderr). On stop verify owned PIDs gone and report whether the port is free; do not kill a foreign replacement listener.

Report exact DB, origin, evidence/state path and stopped/running status. Startup/health does not prove UI workflows, student personal use, NFR or course acceptance. New CLI session discovery/invocation of `/run-app` is a separate observation, not established by writing/validating this file.
