"""Admin-only safe audit queries; workflow scope remains in workflow service."""

import json

from 主程序.repositories.database import database_transaction
from 主程序.repositories.workflow_repository import require_workflow
from 主程序.services.workflow_service import authorize


def runs(database, context, **filters):
    from 主程序.repositories import pipeline_repository
    authorize(context.actor, 'read')
    with database_transaction(database, readonly=True) as c:
        context.verify(c)
        return pipeline_repository.list_runs(c, context.actor, **filters)


def users(database, context, **filters):
    from 主程序.repositories import user_repository
    authorize(context.actor, 'users')
    with database_transaction(database, readonly=True) as c:
        context.verify(c)
        return user_repository.list_users(c, **filters)


def audits(database, actor, *, target_type=None, target_id=None, page=1, page_size=50):
    authorize(actor, "audit")
    with database_transaction(database, readonly=True) as c:
        require_workflow(c)
        clauses, params = ["1=1"], []
        for field, value in (("target_type", target_type), ("target_id", target_id)):
            if value is not None:
                clauses.append(field + "=?")
                params.append(value)
        where = " AND ".join(clauses)
        total = c.execute("SELECT COUNT(*) FROM audit_events WHERE " + where, params).fetchone()[0]
        cur = c.execute("SELECT id,actor_id,request_id,target_type,target_id,action,outcome,error_code,summary_json,created_at FROM audit_events WHERE " + where + " ORDER BY created_at,id LIMIT ? OFFSET ?", (*params, page_size, (page - 1) * page_size))
        names = [col[0] for col in cur.description]
        items = [dict(zip(names, row)) for row in cur]
        for item in items:
            item["states"] = json.loads(item.pop("summary_json"))
        return {"items": items, "total": total, "page": page, "page_size": page_size}
