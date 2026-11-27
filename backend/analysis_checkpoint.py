"""Resume the original analysis message with its original inputs and evidence ledger."""

import json

from fastapi import HTTPException

from .database import encode, uid
from .schemas import MessageInput


def scope_stamp(db, conversation_id, model):
    dictionary = db.one(
        "SELECT dictionary_id,fingerprint,json_extract(payload,'$.business_dictionary.revision') AS revision "
        "FROM current_model_semantics WHERE model_id=?",
        (model["id"],),
    )
    bindings = db.all(
        "SELECT literal,table_name,column_name FROM conversation_bindings "
        "WHERE conversation_id=? ORDER BY literal",
        (conversation_id,),
    )
    return [
        model["id"],
        model["fingerprint"],
        model.get("port"),
        (model.get("report") or {}).get("engine_pid"),
        dictionary,
        bindings,
    ]


def load_checkpoint(db, conversation_id, message_id, version):
    row = db.one(
        "SELECT c.payload FROM analysis_checkpoints c JOIN messages m ON m.id=c.message_id "
        "WHERE c.message_id=? AND c.version=? AND m.conversation_id=? AND m.rowid="
        "(SELECT MAX(rowid) FROM messages WHERE conversation_id=?)",
        (message_id, version, conversation_id, conversation_id),
    )
    if not row:
        raise HTTPException(409, "续跑记录已更新，请刷新会话后查看最新进度")
    return {**json.loads(row["payload"]), "message_id": message_id, "version": version}


def original_request(saved, request):
    return MessageInput.model_validate(
        {
            **saved["body"],
            "generation_id": request.generation_id,
            "llm_profile_id": request.llm_profile_id or saved["body"].get("llm_profile_id"),
        }
    )


def remaining_tasks(saved, records):
    previous = set(saved["completed_ids"])
    completed = {
        task
        for r in records
        if r["id"] not in previous and r["ok"] and r.get("run_verified")
        for task in (r["title"], *r.get("resolved_tasks", []))
    }
    return [task for task in saved["pending"] if task not in completed]


def claim_checkpoint(db, conversation_id, model, saved):
    # Called under the conversation lock, after checking the live model.
    if scope_stamp(db, conversation_id, model) != saved["source"]:
        raise HTTPException(409, "模型连接、快照或已确认口径已变化，请根据当前模型重新分析")
    if not db.execute(
        "UPDATE analysis_checkpoints SET version=? WHERE message_id=? AND version=? AND message_id="
        "(SELECT id FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1)",
        (uid(), saved["message_id"], saved["version"], conversation_id),
    ):
        raise HTTPException(409, "该续跑请求已经处理，请刷新会话")


def save_checkpoint(run, planner, pending):
    fields = planner.fields
    payload = {
        "source": run.source_stamp,
        "user_id": run.user_id,
        "body": run.body.model_dump(exclude={"generation_id"}),
        "window": {"messages": run.window.messages, "notes": run.window.notes},
        "template": bool(run.template),
        "scope": run.scope,
        "pending": list(dict.fromkeys(pending)),
        "completed_ids": [q["id"] for q in run.evidence if q["ok"]],
        "recent_issues": planner.recent_issues,
        "prior": fields.prior,
        "selected_choice": planner.selected_choice,
        "reused": list(fields.reused),
        "semantic_records": planner.semantic_records,
        "semantic_closed": planner.semantic_closed,
        "terms": fields.terms,
        "settled": sorted(fields.settled),
        "member_lists": fields.member_lists,
        "unchecked": {
            k: [[t, c, reason] for (t, c), reason in v.items()] for k, v in fields.unchecked.items()
        },
        "redirected_searches": sorted(fields.redirected_searches),
        "unread_searches": fields.unread_searches,
    }
    version = uid()
    run.db.execute(
        "INSERT INTO analysis_checkpoints VALUES (?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
        "version=excluded.version,payload=excluded.payload",
        (run.message_id, version, encode(payload)),
    )
    return version


def restore_planner(planner, saved):
    fields = planner.fields
    fields.records = planner.run.service.field_records(saved["message_id"])
    fields.rounds = max((r.get("round", 0) for r in fields.records), default=0)
    fields.prior = saved["prior"]
    fields.reused = {r["id"]: r for r in fields.prior if r["id"] in saved["reused"]}
    fields.terms = saved["terms"]
    fields.settled = set(saved["settled"])
    fields.member_lists = saved["member_lists"]
    fields.unchecked = {
        k: {(t, c): reason for t, c, reason in items} for k, items in saved["unchecked"].items()
    }
    fields.redirected_searches = set(saved["redirected_searches"])
    fields.unread_searches = saved["unread_searches"]
    planner.selected_choice = saved["selected_choice"]
    planner.recent_issues = saved.get("recent_issues", [])
    planner.semantic_records = saved["semantic_records"]
    planner.semantic_closed = saved["semantic_closed"]
