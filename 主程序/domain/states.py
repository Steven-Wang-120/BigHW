"""Pure work-order transitions; no persistence, HTTP or matching changes."""

from dataclasses import dataclass, replace


class RuleError(ValueError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.code, self.details = code, details


@dataclass(frozen=True)
class WorkState:
    status: str = "PENDING"
    version: int = 0
    assignee_id: str | None = None
    resolution_note: str | None = None


def transition(state: WorkState, event: str, expected_version: int, *, assignee_id=None, resolution_note=None):
    if event == "assign" and state.assignee_id is not None:
        raise RuleError("ALREADY_ASSIGNED", current_assignee=state.assignee_id)
    if expected_version != state.version:
        raise RuleError("VERSION_CONFLICT", current_version=state.version, current_state=state.status)
    if event == "assign":
        if state.status != "PENDING":
            raise RuleError("INVALID_TRANSITION")
        if not assignee_id:
            raise RuleError("INVALID_ASSIGNEE")
        return replace(state, assignee_id=assignee_id, version=state.version + 1)
    if state.assignee_id is None:
        raise RuleError("NOT_ASSIGNED")
    if event == "start" and state.status == "PENDING":
        return replace(state, status="IN_PROGRESS", version=state.version + 1)
    if event == "close" and state.status == "IN_PROGRESS":
        if not isinstance(resolution_note, str) or not resolution_note.strip():
            raise RuleError("EMPTY_RESOLUTION")
        return replace(state, status="CLOSED", resolution_note=resolution_note.strip(), version=state.version + 1)
    raise RuleError("INVALID_TRANSITION")
