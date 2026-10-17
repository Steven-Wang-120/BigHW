"""Deny-only literal command guard; not a shell parser or a security sandbox.

PreToolUse stdin -> official deny JSON on a match, otherwise silence + exit 0.
No command is executed. No permissions/updatedInput/allow output is emitted.
Only fixed reason/rule, UTC time, tool and command hash are logged locally.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
MAX_INPUT_BYTES = 65536
TOOLS = {"Bash", "PowerShell"}
# Best-effort literal patterns: quoted examples can also match. This does not
# decode shell expansions, aliases/functions, encoded commands or script files.
TAIL = r"[^;\n|]*?"
END = r"(?=$|[\s:'\";|)])"
RULES = (
    ("recursive_remove_item", r"\bremove-item\b" + TAIL + r"(?<!\S)-(?:r|recurse)" + END),
    ("recursive_rm", r"(?<![\w.-])rm(?:\.exe)?\s+" + TAIL + r"(?<!\S)(?:-[a-z]*r[a-z]*|--recursive)" + END),
    ("recursive_rmdir", r"\b(?:rmdir|rd)(?:\.exe)?\s+" + TAIL + r"(?<!\S)/s" + END),
    ("git_reset_hard", r"\bgit(?:\.exe)?\s+" + TAIL + r"\breset\s+" + TAIL + r"(?<!\S)--hard" + END),
    ("git_clean_force", r"\bgit(?:\.exe)?\s+" + TAIL + r"\bclean\s+" + TAIL + r"(?<!\S)(?:-[a-z]*f[a-z]*|--force)" + END),
    ("git_push_force", r"\bgit(?:\.exe)?\s+" + TAIL + r"\bpush\s+" + TAIL + r"(?<!\S)(?:--force(?:-with-lease(?:=[^\s;|]*)?)?|-f)" + END),
    ("execution_policy_bypass", r"(?<![\w-])-(?:executionpolicy|ep|exec)\s*(?:[:=]\s*|\s+)['\"]?bypass" + END),
    ("set_execution_policy_bypass", r"\bset-executionpolicy\s+" + TAIL + r"\bbypass" + END),
)
PATTERNS = [(name, re.compile(pattern, re.I)) for name, pattern in RULES]


def denial(tool, pattern, command_hash):
    reason = "Project danger guard denied: " + pattern + ". No command executed; retain normal MARINA approval."
    record = {"utc": datetime.now(timezone.utc).isoformat(), "tool": tool,
              "pattern": pattern, "command_sha256": command_hash, "reason": reason}
    try:
        # The log root is anchored to this script, not cwd/stdin/environment.
        parent = ROOT / "数据/runtime/B10-guard" / datetime.now().astimezone().date().isoformat()
        parent.mkdir(parents=True, exist_ok=True)
        directory = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex)
        directory.mkdir()
        (directory / "denial.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    except OSError:
        # Official exit-2 blocking fallback; never leak an OS error/path/input.
        print("Project danger guard could not persist denial metadata; command blocked.", file=sys.stderr)
        return 2
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                      "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    return 0


def main():
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return denial("INVALID", "input_too_large", None)
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate field")
                result[key] = value
            return result
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
            raise ValueError("invalid event")
        tool = payload.get("tool_name")
        if not isinstance(tool, str):
            raise ValueError("invalid tool")
        if tool not in TOOLS:
            return 0
        fields = payload.get("tool_input")
        if not isinstance(fields, dict) or not isinstance(fields.get("command"), str):
            raise ValueError("invalid command")
        command = fields["command"]
        if not command.strip() or "\0" in command:
            raise ValueError("invalid command")
    except (ValueError, UnicodeError, RecursionError):
        return denial("INVALID", "invalid_hook_input", None)
    try:
        command_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()
    except UnicodeError:
        return denial(tool, "invalid_hook_input", None)
    for name, pattern in PATTERNS:
        if pattern.search(command):
            return denial(tool, name, command_hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
