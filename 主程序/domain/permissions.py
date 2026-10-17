"""Trusted internal actors and pure role/object-scope decisions."""

from dataclasses import dataclass

ROLES = frozenset({"FINANCE", "SUPERVISOR", "HANDLER", "ADMIN"})
BUSINESS_READ = frozenset({"FINANCE", "SUPERVISOR", "HANDLER"})
PERMISSIONS = {
    "read": BUSINESS_READ, "assign": frozenset({"SUPERVISOR"}),
    "handlers": frozenset({"SUPERVISOR"}), "start": frozenset({"HANDLER"}),
    "close": frozenset({"HANDLER"}), "users": frozenset({"ADMIN"}),
    "audit": frozenset({"ADMIN"}), "import": frozenset({"FINANCE"}),
    "run": frozenset({"FINANCE"}), "import_read": frozenset({"FINANCE", "SUPERVISOR"}),
}


@dataclass(frozen=True)
class Actor:
    id: str
    role: str


def allowed(actor: Actor, action: str) -> bool:
    return actor.role in PERMISSIONS[action]


def in_scope(actor: Actor, assignee_id: str | None) -> bool:
    return actor.role in {"FINANCE", "SUPERVISOR"} or (actor.role == "HANDLER" and actor.id == assignee_id)
