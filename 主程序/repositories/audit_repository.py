"""Append-only B05/B06 audit writes; caller owns success transactions."""

from uuid import uuid4

from 主程序.domain.serialization import json_text
from 主程序.repositories.import_repository import now
from 主程序.repositories.xlsx_reader import InputError


def append(c, actor_id, request_id, target_type, target_id, action, outcome, *, code=None, before=None, after=None):
    try:
        c.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (uuid4().hex, actor_id, request_id, target_type, target_id, action, outcome, code,
                   json_text({"before": before, "after": after}), now()))
    except Exception as exc:
        raise InputError("AUDIT_WRITE_FAILED", 500, audit_persisted=False) from exc
